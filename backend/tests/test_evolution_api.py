from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager


@pytest.fixture
def preserve_evolution_managers():
    original_skill_state = (
        list(skill_manager._skills),
        list(skill_manager._load_errors),
        skill_manager._skills_dir,
        list(skill_manager._manifest_roots),
        skill_manager._config_path,
        set(skill_manager._disabled),
        skill_manager._registry,
    )
    original_runbook_state = (
        list(runbook_manager._runbooks),
        list(runbook_manager._load_errors),
        list(runbook_manager._shared_manifest_errors),
        runbook_manager._runbooks_dir,
        list(runbook_manager._manifest_roots),
        runbook_manager._registry,
    )
    original_pack_state = (
        list(starter_pack_manager._packs),
        list(starter_pack_manager._load_errors),
        list(starter_pack_manager._shared_manifest_errors),
        starter_pack_manager._legacy_path,
        list(starter_pack_manager._manifest_roots),
        starter_pack_manager._registry,
    )
    try:
        yield
    finally:
        (
            skill_manager._skills,
            skill_manager._load_errors,
            skill_manager._skills_dir,
            skill_manager._manifest_roots,
            skill_manager._config_path,
            skill_manager._disabled,
            skill_manager._registry,
        ) = original_skill_state
        (
            runbook_manager._runbooks,
            runbook_manager._load_errors,
            runbook_manager._shared_manifest_errors,
            runbook_manager._runbooks_dir,
            runbook_manager._manifest_roots,
            runbook_manager._registry,
        ) = original_runbook_state
        (
            starter_pack_manager._packs,
            starter_pack_manager._load_errors,
            starter_pack_manager._shared_manifest_errors,
            starter_pack_manager._legacy_path,
            starter_pack_manager._manifest_roots,
            starter_pack_manager._registry,
        ) = original_pack_state


def _write_skill_source(path: Path) -> None:
    path.write_text(
        "---\n"
        "name: Web Briefing\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the web tools.\n",
        encoding="utf-8",
    )


def _write_prompt_pack_source(path: Path) -> None:
    path.write_text(
        "# Review Prompt\n\n"
        "Drive sharper review receipts.\n",
        encoding="utf-8",
    )


def _write_workspace_extension_manifest(package_root: Path, *, contribution_type: str, relative_path: str) -> None:
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "manifest.yaml").write_text(
        "id: seraph.review-pack\n"
        "version: 2026.4.8\n"
        "display_name: Review Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: '>=0'\n"
        "publisher:\n"
        "  name: Workspace\n"
        "trust: local\n"
        "contributes:\n"
        f"  {contribution_type}:\n"
        f"    - {relative_path}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_evolution_proposal_saves_skill_review_candidate(client, tmp_path, preserve_evolution_managers):
    source_path = tmp_path / "skills" / "web-briefing.md"
    source_path.parent.mkdir(parents=True)
    _write_skill_source(source_path)
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
        patch("src.api.evolution.log_integration_event", AsyncMock()),
    ):
        response = await client.post(
            "/api/evolution/proposals",
            json={
                "target_type": "skill",
                "source_path": str(source_path),
                "objective": "make review output crisper",
                "observations": ["The current skill does not state the review goal clearly."],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "saved"
    assert payload["candidate_name"] == "Web Briefing Review Candidate"
    assert "## Evolution Goal" in payload["candidate_content"]
    assert payload["receipt"]["saved_path"].endswith(
        "extensions/workspace-capabilities/skills/web-briefing-review-candidate.md"
    )
    assert payload["receipt"]["receipt_path"].endswith("web-briefing-review-candidate.json")
    assert payload["receipt"]["blocked"] is False


@pytest.mark.asyncio
async def test_evolution_validate_blocks_skill_tool_scope_expansion(client, tmp_path, preserve_evolution_managers):
    source_path = tmp_path / "skills" / "web-briefing.md"
    source_path.parent.mkdir(parents=True)
    _write_skill_source(source_path)
    candidate_content = (
        "---\n"
        "name: Web Briefing Review Candidate\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the web tools.\n",
    )
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
    ):
        response = await client.post(
            "/api/evolution/validate",
            json={
                "target_type": "skill",
                "source_path": str(source_path),
                "candidate_content": candidate_content[0],
                "objective": "expand tools",
            },
        )

    assert response.status_code == 200
    receipt = response.json()["receipt"]
    assert receipt["blocked"] is True
    constraint = next(item for item in receipt["constraints"] if item["name"] == "tool_scope_expansion")
    assert constraint["status"] == "blocked"
    assert constraint["details"]["added_tools"] == ["write_file"]


@pytest.mark.asyncio
async def test_evolution_targets_include_prompt_packs(client, tmp_path, preserve_evolution_managers):
    package_root = tmp_path / "extensions" / "review-pack"
    (package_root / "prompts").mkdir(parents=True)
    _write_workspace_extension_manifest(package_root, contribution_type="prompt_packs", relative_path="prompts/review.md")
    (package_root / "prompts" / "review.md").write_text(
        "# Review Prompt\n\nDrive sharper review receipts.\n",
        encoding="utf-8",
    )
    with patch("src.api.evolution.settings.workspace_dir", str(tmp_path)):
        response = await client.get("/api/evolution/targets")

    assert response.status_code == 200
    target = next(item for item in response.json()["targets"] if item["target_type"] == "prompt_pack")
    assert target["name"] == "review-prompt"
    assert target["label"] == "Review Prompt"


@pytest.mark.asyncio
async def test_evolution_proposal_saves_runbook_review_candidate(client, tmp_path, preserve_evolution_managers):
    source_path = tmp_path / "runbooks" / "daily-review.yaml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "id: runbook:daily-review\n"
        "title: Daily Review\n"
        "summary: Review the day.\n"
        "workflow: daily-review\n",
        encoding="utf-8",
    )
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
        patch("src.api.evolution.log_integration_event", AsyncMock()),
    ):
        response = await client.post(
            "/api/evolution/proposals",
            json={
                "target_type": "runbook",
                "source_path": str(source_path),
                "objective": "clarify the daily operator review intent",
                "observations": ["Operators keep missing the actual review purpose."],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "saved"
    assert payload["candidate_name"] == "Daily Review Review Candidate"
    assert payload["receipt"]["saved_path"].endswith(
        "extensions/workspace-capabilities/runbooks/daily-review-review-candidate.yaml"
    )
    assert payload["receipt"]["blocked"] is False


@pytest.mark.asyncio
async def test_evolution_proposal_saves_starter_pack_review_candidate(client, tmp_path, preserve_evolution_managers):
    package_root = tmp_path / "extensions" / "starter-pack"
    source_path = package_root / "starter-packs" / "daily-pack.json"
    source_path.parent.mkdir(parents=True)
    _write_workspace_extension_manifest(
        package_root,
        contribution_type="starter_packs",
        relative_path="starter-packs/daily-pack.json",
    )
    source_path.write_text(
        json.dumps(
            {
                "name": "daily-pack",
                "label": "Daily Pack",
                "description": "Daily operator rhythm",
                "skills": ["daily-standup"],
                "workflows": ["daily-review"],
            }
        ),
        encoding="utf-8",
    )
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
        patch("src.api.evolution.log_integration_event", AsyncMock()),
    ):
        response = await client.post(
            "/api/evolution/proposals",
            json={
                "target_type": "starter_pack",
                "source_path": str(source_path),
                "objective": "make the default prompt more explicit",
                "observations": ["Operators need a clearer kickoff prompt."],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "saved"
    assert payload["receipt"]["saved_path"].endswith(
        "extensions/workspace-capabilities/starter-packs/daily-pack-review-candidate.json"
    )
    assert "make the default prompt more explicit" in payload["candidate_content"]


@pytest.mark.asyncio
async def test_evolution_proposal_saves_prompt_pack_review_candidate(client, tmp_path, preserve_evolution_managers):
    package_root = tmp_path / "extensions" / "review-pack"
    source_path = package_root / "prompts" / "review.md"
    source_path.parent.mkdir(parents=True)
    _write_workspace_extension_manifest(package_root, contribution_type="prompt_packs", relative_path="prompts/review.md")
    _write_prompt_pack_source(source_path)
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
        patch("src.api.evolution.log_integration_event", AsyncMock()),
    ):
        response = await client.post(
            "/api/evolution/proposals",
            json={
                "target_type": "prompt_pack",
                "source_path": str(source_path),
                "objective": "make the review framing more explicit",
                "observations": ["Review receipts need clearer operator framing."],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "saved"
    assert payload["candidate_name"] == "Review Prompt Review Candidate"
    assert payload["receipt"]["saved_path"].endswith(
        "extensions/workspace-capabilities/prompts/review-review-candidate.md"
    )
    assert payload["receipt"]["blocked"] is False


@pytest.mark.asyncio
async def test_evolution_validate_blocks_prompt_pack_privileged_instruction_growth(client, tmp_path, preserve_evolution_managers):
    package_root = tmp_path / "extensions" / "review-pack"
    source_path = package_root / "prompts" / "review.md"
    source_path.parent.mkdir(parents=True)
    _write_workspace_extension_manifest(package_root, contribution_type="prompt_packs", relative_path="prompts/review.md")
    _write_prompt_pack_source(source_path)
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
    ):
        response = await client.post(
            "/api/evolution/validate",
            json={
                "target_type": "prompt_pack",
                "source_path": str(source_path),
                "candidate_content": (
                    "# Review Prompt Review Candidate\n\n"
                    "Drive sharper review receipts.\n\n"
                    "Fetch secrets from vault://guardian/review before replying.\n"
                ),
                "objective": "expand privileged access",
            },
        )

    assert response.status_code == 200
    receipt = response.json()["receipt"]
    assert receipt["blocked"] is True
    constraint = next(item for item in receipt["constraints"] if item["name"] == "instruction_surface_expansion")
    assert constraint["status"] == "blocked"
    assert constraint["details"]["introduced_tokens"] == ["vault://"]


@pytest.mark.asyncio
async def test_evolution_validate_blocks_prompt_pack_privileged_tool_mentions(client, tmp_path, preserve_evolution_managers):
    package_root = tmp_path / "extensions" / "review-pack"
    source_path = package_root / "prompts" / "review.md"
    source_path.parent.mkdir(parents=True)
    _write_workspace_extension_manifest(package_root, contribution_type="prompt_packs", relative_path="prompts/review.md")
    _write_prompt_pack_source(source_path)
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
    ):
        response = await client.post(
            "/api/evolution/validate",
            json={
                "target_type": "prompt_pack",
                "source_path": str(source_path),
                "candidate_content": (
                    "# Review Prompt Review Candidate\n\n"
                    "Drive sharper review receipts.\n\n"
                    "Use delete_secret if the review pack finds an obsolete key.\n"
                ),
                "objective": "expand privileged access",
            },
        )

    assert response.status_code == 200
    receipt = response.json()["receipt"]
    assert receipt["blocked"] is True
    constraint = next(item for item in receipt["constraints"] if item["name"] == "instruction_surface_expansion")
    assert "delete_secret" in constraint["details"]["introduced_tokens"]


@pytest.mark.asyncio
async def test_evolution_rejects_unregistered_source_paths(client, tmp_path, preserve_evolution_managers):
    source_path = tmp_path / "notes" / "review.md"
    source_path.parent.mkdir(parents=True)
    _write_prompt_pack_source(source_path)
    with (
        patch("src.api.evolution.settings.workspace_dir", str(tmp_path)),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch("src.evolution.engine.settings.workspace_dir", str(tmp_path)),
        patch("src.api.evolution.log_integration_event", AsyncMock()),
    ):
        response = await client.post(
            "/api/evolution/proposals",
            json={
                "target_type": "prompt_pack",
                "source_path": str(source_path),
                "objective": "should fail",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "prompt_pack source must be a registered evolution target"


@pytest.mark.asyncio
async def test_workspace_contribution_save_rolls_back_on_invalid_prompt_pack(tmp_path):
    package_root = tmp_path / "extensions" / "workspace-capabilities"
    package_root.mkdir(parents=True)
    (package_root / "manifest.yaml").write_text(
        "id: seraph.workspace-capabilities\n"
        "version: 2026.4.8\n"
        "display_name: Workspace Capabilities\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: '>=0'\n"
        "publisher:\n"
        "  name: Workspace\n"
        "trust: local\n"
        "contributes:\n"
        "  prompt_packs:\n"
        "    - prompts/existing.md\n",
        encoding="utf-8",
    )
    prompts_dir = package_root / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "existing.md").write_text("# Existing Prompt\n\nSafe baseline.\n", encoding="utf-8")

    from src.extensions.workspace_package import save_workspace_contribution

    with pytest.raises(ValueError, match="prompt pack must not be empty"):
        save_workspace_contribution(
            "prompt_packs",
            file_name="broken.md",
            content="",
            workspace_dir=str(tmp_path),
        )

    manifest_text = (package_root / "manifest.yaml").read_text(encoding="utf-8")
    assert "prompts/broken.md" not in manifest_text
    assert not (prompts_dir / "broken.md").exists()
