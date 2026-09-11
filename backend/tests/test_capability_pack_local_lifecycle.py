"""Actual provider-free local lifecycle proof for capability-pack v2."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.extensions.capability_pack import (
    CapabilityPackLifecycle,
    CapabilityPackLifecycleError,
    capability_pack_digest,
    load_capability_pack_workflows,
    parse_capability_pack_manifest,
    validate_capability_pack_dependencies,
)


def _manifest() -> str:
    return """schema_version: 2
id: seraph.local-proof-pack
version: 1.0.0
kind: capability-pack
publisher:
  name: Seraph
  provenance: local-reviewed
signature:
  state: unsigned-local
  signer: null
compatibility:
  seraph: ">=1"
dependencies: []
contributes:
  capabilities: [research-brief, goal-snapshot]
  skills: []
  workflows: [workflows/local-research.md, workflows/local-snapshot.md]
  prompts: []
  sources: []
  reports: []
  evals: []
  runbooks: []
authority:
  tools: [read_file, write_file, get_goals, web_search]
  filesystem: [workspace_read, workspace_write, artifact_write]
  network: false
  secrets: []
  approval: always
resources:
  inference_priority: approved_operator
  max_inference_cost_microusd: 0
  max_runtime_seconds: 300
  max_artifact_bytes: 10485760
data_policy:
  classes: [public]
  egress: []
policy_overlays: []
lifecycle:
  hooks:
    activate: required
    pause: required
    update: required
    revoke: required
    uninstall: required
  artifact_migration: preserve
  revoke_running_jobs: cancel_at_safe_checkpoint
"""


def _workflow(name: str, source_tool: str) -> str:
    return f"""---
name: {name}
description: Local governed workflow proof.
requires:
  tools: [{source_tool}, write_file]
inputs:
  file_path:
    type: string
    description: Output artifact path.
steps:
  - id: source
    tool: {source_tool}
    arguments: {{}}
  - id: save
    tool: write_file
    arguments:
      file_path: \"{{{{ file_path }}}}\"
      content: \"{{{{ steps.source.result }}}}\"
result: Local artifact written.
---

Local-only declarative workflow.
"""


def _package(tmp_path: Path) -> tuple[Path, object]:
    root = tmp_path / "local-proof-pack"
    (root / "workflows").mkdir(parents=True)
    (root / "manifest.yaml").write_text(_manifest(), encoding="utf-8")
    (root / "workflows" / "local-research.md").write_text(
        _workflow("local-research-brief", "web_search"), encoding="utf-8"
    )
    (root / "workflows" / "local-snapshot.md").write_text(
        _workflow("local-goal-snapshot", "get_goals"), encoding="utf-8"
    )
    return root, parse_capability_pack_manifest(_manifest())


def _activate(store: CapabilityPackLifecycle, root: Path, pack, *, owner: str, session: str):
    review = store.review(pack, root_path=root, goal_id="goal-local", reviewed_by=owner)
    approval = store.create_operator_approval(
        pack.id,
        action="activate",
        goal_id="goal-local",
        digest=review["review"]["digest"],
        version=pack.version,
        owner_principal_id=owner,
        session_id=session,
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )
    store.activate(
        pack,
        root_path=root,
        goal_id="goal-local",
        review_id=review["review"]["review_id"],
        approval_id=approval["approval"]["approval_id"],
        owner_principal_id=owner,
        session_id=session,
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )
    return review


def test_local_two_domain_execution_is_real_and_intercepted(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:local-proof"
    session = "session-local-proof"
    review = _activate(store, root, pack, owner=owner, session=session)
    calls: list[str] = []

    def transport(url: str, **_: object) -> dict[str, str]:
        calls.append(url)
        return {"title": "Controlled source", "content": "Evidence from the intercepted source."}

    primary = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-primary",
        domain="primary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="research.md",
        owner_principal_id=owner,
        session_id=session,
        source_url="http://controlled.test/source",
        query="local proof",
        intercepted_transport=transport,
    )
    secondary = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-secondary",
        domain="secondary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="snapshot.md",
        owner_principal_id=owner,
        session_id=session,
        goal_snapshot={"goal_id": "goal-local", "revision": 2, "title": "Keep proof bounded"},
    )

    assert calls == ["http://controlled.test/source"]
    assert primary["execution"]["transport"] == "intercepted"
    assert primary["execution"]["live_network_calls"] == 0
    assert primary["execution"]["artifact"]["readback_ok"] is True
    assert secondary["execution"]["transport"] == "none"
    assert secondary["execution"]["artifact"]["readback_ok"] is True
    assert Path(primary["execution"]["artifact"]["path"]).read_text().find("Controlled source") >= 0
    assert Path(secondary["execution"]["artifact"]["path"]).read_text().find("goal-local") >= 0
    assert len(store.status(pack.id)["local_executions"]) == 2
    assert review["review"]["digest"] == capability_pack_digest(root)
    deduped = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-primary",
        domain="primary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="research.md",
        owner_principal_id=owner,
        session_id=session,
        source_url="http://controlled.test/source",
        query="local proof",
        intercepted_transport=transport,
    )
    assert deduped["status"] == "deduped"
    assert calls == ["http://controlled.test/source"]


def test_authenticated_approval_binds_owner_session_content_and_authority(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    review = store.review(pack, root_path=root, goal_id="goal-local", reviewed_by="operator:one")
    approval = store.create_operator_approval(
        pack.id,
        action="activate",
        goal_id="goal-local",
        digest=review["review"]["digest"],
        owner_principal_id="operator:one",
        session_id="session-one",
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )["approval"]
    with pytest.raises(CapabilityPackLifecycleError, match="exact goal/digest/authority"):
        store.activate(
            pack,
            root_path=root,
            goal_id="goal-local",
            review_id=review["review"]["review_id"],
            approval_id=approval["approval_id"],
            owner_principal_id="operator:two",
            session_id="session-one",
            content_digest=review["review"]["digest"],
            authority_digest=pack.authority_digest,
        )


def test_reconcile_blocks_interrupted_running_job_and_dependency_validator_is_exact(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    _activate(store, root, pack, owner="operator:local-proof", session="session-local-proof")
    store.register_job(pack.id, goal_id="goal-local", job_id="job-crashed")
    store._set_local_job_status("job-crashed", status="running", details={"execution_mode": "local_functional"})
    reconciliation = store.reconcile(pack.id)
    assert reconciliation["status"] == "blocked"
    assert store.status(pack.id)["jobs"][0]["reconciliation_required"] is True
    dependency_manifest = json.loads(json.dumps(pack.model_dump(mode="json")))
    dependency_manifest["dependencies"] = [{"id": "seraph.base", "version": ">=1", "digest": "a" * 64}]
    assert validate_capability_pack_dependencies(dependency_manifest, {"seraph.base": {"version": ">=1", "digest": "a" * 64}}) == ()
    assert validate_capability_pack_dependencies(dependency_manifest, {})


def test_workflow_allowlist_rejects_executable_steps_and_revoke_cancels_pinned_job(tmp_path: Path):
    root, pack = _package(tmp_path)
    (root / "workflows" / "local-snapshot.md").write_text(
        _workflow("local-goal-snapshot", "shell"), encoding="utf-8"
    )
    with pytest.raises(CapabilityPackLifecycleError, match="unsupported local tools"):
        load_capability_pack_workflows(root, pack)

    root, pack = _package(tmp_path / "revoke")
    store = CapabilityPackLifecycle(tmp_path / "revoke-state.json")
    owner = "operator:local-proof"
    session = "session-local-proof"
    review = _activate(store, root, pack, owner=owner, session=session)
    store.register_job(pack.id, goal_id="goal-local", job_id="job-pinned", owner_principal_id=owner, session_id=session)
    approval = store.create_operator_approval(
        pack.id,
        action="revoke",
        goal_id="goal-local",
        digest=review["review"]["digest"],
        version=pack.version,
        owner_principal_id=owner,
        session_id=session,
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )["approval"]["approval_id"]
    revoked = store.revoke(
        pack.id,
        approval_id=approval,
        owner_principal_id=owner,
        session_id=session,
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )
    assert revoked["status"] == "revoked"
    assert store.status(pack.id)["jobs"][0]["status"] == "cancelled"


def test_operator_api_routes_are_registered_for_readback_and_local_controls():
    from src.api.router import api_router

    paths = {route.path for route in api_router.routes}
    assert "/api/capability-packs/{pack_id}" in paths
    assert "/api/capability-packs/{pack_id}/reconcile" in paths
    assert "/api/capability-packs/{pack_id}/execute-local" in paths
