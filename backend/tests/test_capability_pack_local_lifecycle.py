"""Actual provider-free local lifecycle proof for capability-pack v2."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

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


def _package(tmp_path: Path, *, manifest_text: str | None = None) -> tuple[Path, object]:
    root = tmp_path / "local-proof-pack"
    (root / "workflows").mkdir(parents=True)
    text = manifest_text or _manifest()
    (root / "manifest.yaml").write_text(text, encoding="utf-8")
    (root / "workflows" / "local-research.md").write_text(
        _workflow("local-research-brief", "web_search"), encoding="utf-8"
    )
    (root / "workflows" / "local-snapshot.md").write_text(
        _workflow("local-goal-snapshot", "get_goals"), encoding="utf-8"
    )
    return root, parse_capability_pack_manifest(text)


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


def _goal_snapshot(owner: str, session: str, *, revision: int = 1, **values: object) -> dict[str, object]:
    return {
        "goal_id": "goal-local",
        "revision": revision,
        "status": "active",
        "owner_principal_id": owner,
        "session_id": session,
        "canonical_source": "goals",
        **values,
    }


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
        goal_snapshot=_goal_snapshot(owner, session, revision=2, title="Keep proof bounded"),
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
    assert validate_capability_pack_dependencies(
        dependency_manifest,
        {"seraph.base": {"version": "1.2.0", "digest": "a" * 64}},
    ) == ()
    assert "dependency is revoked" in validate_capability_pack_dependencies(
        dependency_manifest,
        {"seraph.base": {"version": "1.2.0", "digest": "a" * 64, "revoked": True}},
    )[0]
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
    store._set_local_job_status("job-pinned", status="running")
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
    assert "/api/capability-packs/{pack_id}/reconcile/resolve" in paths
    assert "/api/capability-packs/{pack_id}/execute-local" in paths


def test_local_execution_fails_closed_for_empty_authority_and_unsafe_artifact_paths(tmp_path: Path):
    manifest_text = _manifest().replace(
        "tools: [read_file, write_file, get_goals, web_search]",
        "tools: []",
    ).replace(
        "filesystem: [workspace_read, workspace_write, artifact_write]",
        "filesystem: []",
    )
    root = tmp_path / "empty-scopes"
    (root / "workflows").mkdir(parents=True)
    (root / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (root / "workflows" / "local-research.md").write_text(_workflow("local-research-brief", "web_search"), encoding="utf-8")
    (root / "workflows" / "local-snapshot.md").write_text(_workflow("local-goal-snapshot", "get_goals"), encoding="utf-8")
    pack = parse_capability_pack_manifest(manifest_text)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    _activate(store, root, pack, owner="operator:scopes", session="session-scopes")
    with pytest.raises(CapabilityPackLifecycleError, match="authority scopes are empty"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-empty-scopes",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="safe.md",
            owner_principal_id="operator:scopes",
            session_id="session-scopes",
            goal_snapshot=_goal_snapshot("operator:scopes", "session-scopes", title="blocked"),
        )

    root, pack = _package(tmp_path / "unsafe")
    store = CapabilityPackLifecycle(tmp_path / "unsafe-state.json")
    _activate(store, root, pack, owner="operator:unsafe", session="session-unsafe")
    with pytest.raises(CapabilityPackLifecycleError, match="must stay within"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-traversal",
            domain="secondary",
            artifact_root=tmp_path / "unsafe-artifacts",
            artifact_path="../escape.md",
            owner_principal_id="operator:unsafe",
            session_id="session-unsafe",
            goal_snapshot=_goal_snapshot("operator:unsafe", "session-unsafe", title="blocked"),
        )
    with pytest.raises(CapabilityPackLifecycleError, match="must be relative"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-absolute",
            domain="secondary",
            artifact_root=tmp_path / "unsafe-artifacts",
            artifact_path=str(tmp_path / "absolute.md"),
            owner_principal_id="operator:unsafe",
            session_id="session-unsafe",
            goal_snapshot=_goal_snapshot("operator:unsafe", "session-unsafe", title="blocked"),
        )
    artifact_root = tmp_path / "symlink-artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    artifact_root.mkdir()
    (artifact_root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CapabilityPackLifecycleError, match="symlink"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-symlink",
            domain="secondary",
            artifact_root=artifact_root,
            artifact_path="link/escape.md",
            owner_principal_id="operator:unsafe",
            session_id="session-unsafe",
            goal_snapshot=_goal_snapshot("operator:unsafe", "session-unsafe", title="blocked"),
        )


def test_recovery_is_sticky_until_authenticated_resolution_and_identity_is_exact(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:recovery"
    session = "session-recovery"
    _activate(store, root, pack, owner=owner, session=session)
    store.register_job(pack.id, goal_id="goal-local", job_id="job-crashed", owner_principal_id=owner, session_id=session)
    store._set_local_job_status("job-crashed", status="running")
    assert store.reconcile(pack.id, owner_principal_id=owner, session_id=session)["status"] == "blocked"
    assert store.reconcile(pack.id, owner_principal_id=owner, session_id=session)["status"] == "blocked"
    with pytest.raises(CapabilityPackLifecycleError, match="blocked until interrupted"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-new",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="new.md",
            owner_principal_id=owner,
            session_id=session,
            goal_snapshot=_goal_snapshot(owner, session, title="still blocked"),
        )
    with pytest.raises(CapabilityPackLifecycleError, match="owner or session"):
        store.resolve_reconciliation(
            pack.id,
            job_id="job-crashed",
            owner_principal_id="operator:other",
            session_id=session,
        )
    resolved = store.resolve_reconciliation(
        pack.id,
        job_id="job-crashed",
        owner_principal_id=owner,
        session_id=session,
    )
    assert resolved["status"] == "clean"
    assert resolved["job"]["status"] == "cancelled"


def test_local_execution_binds_owner_session_and_full_request_idempotency(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:one"
    session = "session-one"
    _activate(store, root, pack, owner=owner, session=session)

    with pytest.raises(CapabilityPackLifecycleError, match="owner or session"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-cross-owner",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="cross.md",
            owner_principal_id="operator:two",
            session_id="session-two",
            goal_snapshot=_goal_snapshot("operator:two", "session-two", title="denied"),
        )

    first = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-idempotent",
        domain="secondary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="first.md",
        owner_principal_id=owner,
        session_id=session,
        goal_snapshot=_goal_snapshot(owner, session, title="first"),
    )
    assert "Goal: goal-local" in Path(first["execution"]["artifact"]["path"]).read_text(encoding="utf-8")
    with pytest.raises(CapabilityPackLifecycleError, match="request fingerprint"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-idempotent",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="second.md",
            owner_principal_id=owner,
            session_id=session,
            goal_snapshot=_goal_snapshot(owner, session, title="changed"),
        )


def test_revoke_leave_pinned_policy_preserves_running_job(tmp_path: Path):
    manifest_text = _manifest().replace(
        "revoke_running_jobs: cancel_at_safe_checkpoint",
        "revoke_running_jobs: leave_pinned_until_completion",
    )
    root, pack = _package(tmp_path, manifest_text=manifest_text)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:leave"
    session = "session-leave"
    review = _activate(store, root, pack, owner=owner, session=session)
    store.register_job(pack.id, goal_id="goal-local", job_id="job-pinned", owner_principal_id=owner, session_id=session)
    store._set_local_job_status("job-pinned", status="running")
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
    result = store.revoke(
        pack.id,
        approval_id=approval,
        owner_principal_id=owner,
        session_id=session,
        content_digest=review["review"]["digest"],
        authority_digest=pack.authority_digest,
    )
    assert result["receipt"]["details"]["revoke_running_jobs"] == "leave_pinned_until_completion"
    # The revoke policy leaves the in-flight row durable until the next
    # status/restart reconciliation, which fences it into operator recovery.
    assert store.reconcile(pack.id, owner_principal_id=owner, session_id=session)["status"] == "blocked"
    assert store.status(pack.id)["jobs"][0]["status"] == "blocked"


def test_succeeded_job_without_execution_receipt_cannot_replay_transport(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:atomic"
    session = "session-atomic"
    _activate(store, root, pack, owner=owner, session=session)
    calls: list[str] = []

    first = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-atomic",
        domain="primary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="brief.md",
        owner_principal_id=owner,
        session_id=session,
        source_url="http://controlled.test/source",
        intercepted_transport=lambda url, **_: {"url": url, "content": "once"},
    )
    state = json.loads((tmp_path / "lifecycle.json").read_text(encoding="utf-8"))
    del state["local_executions"][first["job"]["job_id"]]
    state["jobs"][first["job"]["job_id"]]["status"] = "succeeded"
    (tmp_path / "lifecycle.json").write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(CapabilityPackLifecycleError, match="blocked until interrupted"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-atomic",
            domain="primary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="brief.md",
            owner_principal_id=owner,
            session_id=session,
            source_url="http://controlled.test/source",
            intercepted_transport=lambda url, **_: calls.append(url),
        )
    assert calls == []


def test_goal_snapshot_binding_rejects_mismatch_and_noncanonical_rows(tmp_path: Path):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner = "operator:goal-binding"
    session = "session-goal-binding"
    _activate(store, root, pack, owner=owner, session=session)
    with pytest.raises(CapabilityPackLifecycleError, match="identity conflicts"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-goal-mismatch",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="mismatch.md",
            owner_principal_id=owner,
            session_id=session,
            goal_snapshot={**_goal_snapshot(owner, session), "goal_id": "other-goal"},
        )
    with pytest.raises(CapabilityPackLifecycleError, match="canonical persisted-goal source"):
        store.execute_local(
            pack.id,
            goal_id="goal-local",
            job_id="job-goal-source",
            domain="secondary",
            artifact_root=tmp_path / "artifacts",
            artifact_path="source.md",
            owner_principal_id=owner,
            session_id=session,
            goal_snapshot={**_goal_snapshot(owner, session), "canonical_source": "caller"},
        )


def test_review_does_not_accept_phantom_dependency_from_external_map(tmp_path: Path):
    manifest_text = _manifest().replace(
        "dependencies: []",
        f"dependencies: [{{id: seraph.missing, version: '>=1', digest: {'a' * 64}}}]",
    )
    root, pack = _package(tmp_path, manifest_text=manifest_text)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")

    with pytest.raises(CapabilityPackLifecycleError, match="dependency is unavailable: seraph.missing"):
        store.review(
            pack,
            root_path=root,
            goal_id="goal-local",
            available_dependencies={
                "seraph.missing": {"version": "1.2.0", "digest": "a" * 64},
            },
        )


def test_accepted_job_requires_recovery_and_reconciliation_is_pack_scoped(tmp_path: Path):
    first_root, first = _package(tmp_path / "first")
    second_root, second = _package(
        tmp_path / "second",
        manifest_text=_manifest().replace("id: seraph.local-proof-pack", "id: seraph.second-proof-pack"),
    )
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    first_owner, first_session = "operator:first", "session:first"
    second_owner, second_session = "operator:second", "session:second"
    _activate(store, first_root, first, owner=first_owner, session=first_session)
    _activate(store, second_root, second, owner=second_owner, session=second_session)
    store.register_job(
        first.id,
        goal_id="goal-local",
        job_id="job-accepted-after-restart",
        status="accepted",
        owner_principal_id=first_owner,
        session_id=first_session,
    )
    store.register_job(
        first.id,
        goal_id="goal-local",
        job_id="job-queued-after-restart",
        status="queued",
        owner_principal_id=first_owner,
        session_id=first_session,
    )

    first_reconciliation = store.reconcile(
        first.id,
        owner_principal_id=first_owner,
        session_id=first_session,
    )
    assert first_reconciliation["status"] == "blocked"
    assert first_reconciliation["changes"] == [
        {
            "job_id": "job-accepted-after-restart",
            "status": "blocked",
            "reason": "interrupted_accepted_job_requires_operator_recovery",
        },
        {
            "job_id": "job-queued-after-restart",
            "status": "blocked",
            "reason": "interrupted_accepted_job_requires_operator_recovery",
        },
    ]
    assert {job["status"] for job in store.status(first.id)["jobs"]} == {"blocked"}

    second_reconciliation = store.reconcile(
        second.id,
        owner_principal_id=second_owner,
        session_id=second_session,
    )
    assert second_reconciliation["status"] == "clean"
    assert second_reconciliation["scope"] == second.id
    assert store.status(second.id)["reconciliation"]["status"] == "clean"
    assert store.status(first.id)["reconciliation"]["status"] == "blocked"


def test_dependency_revoke_fences_dependent_pointer_and_jobs(tmp_path: Path):
    base_root, base = _package(
        tmp_path / "base",
        manifest_text=_manifest().replace("id: seraph.local-proof-pack", "id: seraph.base-proof-pack"),
    )
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    base_owner, base_session = "operator:base", "session:base"
    base_review = _activate(store, base_root, base, owner=base_owner, session=base_session)
    base_digest = base_review["review"]["digest"]
    dependent_text = _manifest().replace(
        "id: seraph.local-proof-pack", "id: seraph.dependent-proof-pack"
    ).replace(
        "dependencies: []",
        f"dependencies: [{{id: {base.id}, version: '>=1', digest: {base_digest}}}]",
    )
    dependent_root, dependent = _package(tmp_path / "dependent", manifest_text=dependent_text)
    dependent_owner, dependent_session = "operator:dependent", "session:dependent"
    dependent_review = _activate(
        store,
        dependent_root,
        dependent,
        owner=dependent_owner,
        session=dependent_session,
    )
    store.register_job(
        dependent.id,
        goal_id="goal-local",
        job_id="dependent-pinned-job",
        status="queued",
        owner_principal_id=dependent_owner,
        session_id=dependent_session,
    )
    base_revoke_approval = store.create_operator_approval(
        base.id,
        action="revoke",
        goal_id="goal-local",
        digest=base_digest,
        version=base.version,
        owner_principal_id=base_owner,
        session_id=base_session,
        content_digest=base_digest,
        authority_digest=base.authority_digest,
    )["approval"]["approval_id"]

    revoked = store.revoke(
        base.id,
        approval_id=base_revoke_approval,
        owner_principal_id=base_owner,
        session_id=base_session,
        content_digest=base_digest,
        authority_digest=base.authority_digest,
    )
    dependent_status = store.status(dependent.id)
    assert revoked["receipt"]["details"]["dependency_dependents"]
    assert dependent_status["active"]["status"] == "revoked"
    assert dependent_status["jobs"][0]["status"] == "cancelled"
    assert dependent_review["review"]["digest"] in dependent_status["revoked_digests"]
    with pytest.raises(CapabilityPackLifecycleError, match="revoked|binding is invalid"):
        store.build_execution_contract(
            dependent.id,
            goal_id="goal-local",
            job_id="dependent-after-revoke",
        )


def test_local_artifact_write_and_receipt_commit_are_exclusive_against_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, pack = _package(tmp_path)
    store = CapabilityPackLifecycle(tmp_path / "lifecycle.json")
    owner, session = "operator:artifact-fence", "session:artifact-fence"
    review = _activate(store, root, pack, owner=owner, session=session)
    digest = review["review"]["digest"]
    revoke_approval = store.create_operator_approval(
        pack.id,
        action="revoke",
        goal_id="goal-local",
        digest=digest,
        version=pack.version,
        owner_principal_id=owner,
        session_id=session,
        content_digest=digest,
        authority_digest=pack.authority_digest,
    )["approval"]["approval_id"]

    import src.extensions.capability_pack as capability_pack_module

    original_write = capability_pack_module._write_canary_artifact
    revoke_started = threading.Event()
    revoke_finished = threading.Event()
    revoke_errors: list[BaseException] = []

    def revoke_in_thread() -> None:
        revoke_started.set()
        try:
            store.revoke(
                pack.id,
                approval_id=revoke_approval,
                owner_principal_id=owner,
                session_id=session,
                content_digest=digest,
                authority_digest=pack.authority_digest,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            revoke_errors.append(exc)
        finally:
            revoke_finished.set()

    def gated_write(path: Path, content: bytes) -> None:
        revoke_thread.start()
        assert revoke_started.wait(1)
        # The revoke worker may start, but it must not finish while the
        # authority transaction owns the lifecycle lock.
        assert not revoke_finished.wait(0.2)
        original_write(path, content)

    monkeypatch.setattr(capability_pack_module, "_write_canary_artifact", gated_write)
    revoke_thread = threading.Thread(target=revoke_in_thread)
    result = store.execute_local(
        pack.id,
        goal_id="goal-local",
        job_id="job-artifact-fence",
        domain="primary",
        artifact_root=tmp_path / "artifacts",
        artifact_path="brief.md",
        owner_principal_id=owner,
        session_id=session,
        source_url="http://controlled.test/source",
        intercepted_transport=lambda _url, **_: {"content": "bounded"},
    )
    revoke_thread.join(timeout=2)
    assert not revoke_thread.is_alive()
    assert not revoke_errors
    assert result["status"] == "succeeded"
    assert result["job"]["status"] == "succeeded"
    assert store.status(pack.id)["active"]["status"] == "revoked"


@pytest.mark.asyncio
async def test_capability_pack_api_uses_persisted_goal_content(monkeypatch: pytest.MonkeyPatch):
    from src.api.capability_packs import LocalExecutionRequest, capability_pack_execute_local

    persisted_goal = SimpleNamespace(
        id="goal-local",
        parent_id=None,
        path="/",
        title="Persisted title",
        description="Persisted description",
        level="daily",
        domain="productivity",
        status="active",
        start_date=None,
        due_date=None,
        sort_order=0,
        revision=4,
        success_criterion_json=None,
        proactive_enabled=True,
        created_at=None,
        updated_at=None,
    )
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator:api"),
        session_id="session:api",
    )
    captured: dict[str, object] = {}

    async def get_goal(_goal_id: str):
        return persisted_goal

    class FakeStore:
        def execute_local(self, pack_id: str, **kwargs: object):
            captured["pack_id"] = pack_id
            captured.update(kwargs)
            return {"status": "succeeded"}

    monkeypatch.setattr("src.api.capability_packs._require_authenticated_capability_operator", lambda _request: operator)
    monkeypatch.setattr("src.api.capability_packs.goal_repository.get", get_goal)
    monkeypatch.setattr("src.api.capability_packs._store", lambda: FakeStore())
    request = LocalExecutionRequest(
        goal_id="goal-local",
        job_id="job-api-canonical",
        domain="secondary",
        goal_snapshot={
            "goal_id": "goal-local",
            "revision": 4,
            "owner_principal_id": "operator:api",
            "session_id": "session:api",
            "title": "FORGED TITLE",
            "description": "FORGED DESCRIPTION",
            "canonical_source": "goals",
        },
    )
    result = await capability_pack_execute_local("seraph.local-proof-pack", request, SimpleNamespace())
    snapshot = captured["goal_snapshot"]
    assert result == {"status": "succeeded"}
    assert isinstance(snapshot, dict)
    assert snapshot["title"] == "Persisted title"
    assert snapshot["description"] == "Persisted description"
    assert snapshot["revision"] == 4
    assert "FORGED TITLE" not in json.dumps(snapshot)
