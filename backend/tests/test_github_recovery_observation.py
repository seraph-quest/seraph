"""Canonical observation-only recovery with real owner, consent and GET proof."""
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import json

import httpx
import pytest
from sqlalchemy import update

from config.settings import settings
from src.db import engine
from src.db.models import Goal
from src.extensions.github_consent import GitHubReadbackAuthority
from src.extensions.github_followthrough import GitHubFollowthroughService, GitHubFollowthroughError
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, DurableJobError, durable_job_repository as jobs
from tests.test_github_connection_consent import active_connection
from tests.test_github_followthrough import _prepared
from src.workflows.repo_publication import write_file


async def observation_case(client, monkeypatch, tmp_path, *, semantic_change=None):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    owner, row, binding, _ = await active_connection(client, monkeypatch)
    goal = Goal(id="observation-goal", title="Observe exact GitHub effect", owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"])
    async with engine.get_session() as db: db.add(goal)
    prepared = replace(_prepared(), owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"], conversation_id=owner["session_id"], connection_id=row.id, connection_revision=row.revision, goal_id=goal.id, goal_revision=goal.revision)
    prepared = replace(prepared, job_id="ghfollow_" + prepared.operation_id.hex)
    current = await jobs.admit_job(DurableJobSpec(identity=DurableJobIdentity(job_id=prepared.job_id, job_kind="github_followthrough_v1", owner_kind="user", owner_principal_id=owner["principal_id"], capability_version="1", idempotency_scope="observation-test", idempotency_key="one-exact-effect"),
        session_id=owner["session_id"], operator_session_id=owner["session_id"], goal_id=goal.id, goal_revision=goal.revision,
        inputs={"selected": "exact"}, declared_authority={"capability_id": "work.github-followthrough.v1", "github_consent": binding, "session_id": owner["session_id"], "principal": owner["principal_id"]}))
    current = await jobs.queue_job(current["job_id"], expected_revision=current["revision"])
    current = await jobs.claim_job(current["job_id"], owner="fixture-observation", expected_revision=current["revision"])
    private_path = "github/followthrough/observation-input.json"
    private_bytes = json.dumps(prepared.as_private_payload(), sort_keys=True).encode()
    write_file(private_path, private_bytes)
    fence = {"owner": current["lease"]["owner"], "fencing_token": current["lease"]["fencing_token"]}
    current = await jobs.record_artifact(current["job_id"], file_path=private_path, artifact_type="github_followthrough_input", content=private_bytes, expected_revision=current["revision"], **fence)
    current = await jobs.record_checkpoint(current["job_id"], checkpoint_id="github-followthrough:prepared", state={}, checkpoint_payload={"payload_path": private_path, "payload_sha256": hashlib.sha256(private_bytes).hexdigest()}, expected_revision=current["revision"], **fence)
    current = await jobs.record_effect(current["job_id"], effect_type="github_publication", effect_id="pr-effect", target_path="/repos/acme/example/issues", target_digest=prepared.body_sha256,
        status="intent", owner=current["lease"]["owner"], fencing_token=current["lease"]["fencing_token"], expected_revision=current["revision"])
    current = await jobs.transition_job(current["job_id"], "unknown_external_effect", owner=current["lease"]["owner"], fencing_token=current["lease"]["fencing_token"], expected_revision=current["revision"])
    service = GitHubFollowthroughService()
    fence = await service._reserve_connection(owner_principal_id=owner["principal_id"], connection_id=row.id, expected_revision=row.revision, job_id=current["job_id"])
    read = GitHubReadbackAuthority(owner["principal_id"], owner["session_id"], current["job_id"], current["job_kind"], row.revision, fence, binding)
    calls = []
    payload = {"number": 1, "title": prepared.title, "body": prepared.body}
    if semantic_change == "body": payload["body"] = "Unrelated publication"
    elif semantic_change == "title": payload["title"] = "Unrelated issue"
    elif semantic_change == "native_kind":
        async with engine.get_session() as db:
            from src.db.models import WorkflowRunState
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]).values(job_kind="arbitrary.github-job"))
    async def resolver(*_args): return ["93.184.216.34"]
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(lambda request: calls.append(request.method) or httpx.Response(200, json=payload)))
    path = "/repos/acme/example/issues/1"
    await service._request(path, method="GET", token="fixture-token", authority_check=read.validate, readback_authority=read)
    identity = {"job_id": current["job_id"], "attempt_count": current["attempt_count"], "authority_digest": current["authority_digest"], "effect_id": "pr-effect", "effect_type": "github_publication", "target_path": "/repos/acme/example/issues", "target_digest": prepared.body_sha256, "adapter_idempotency_key": None}
    verified = await service.verified_get_receipt(read_authority=read, path=path, payload=payload, effect_identity=identity)
    observation = {"schema": "seraph.github-effect-observation.v1", **identity, "readback_id": "actual-readback", "verified_at": datetime.now(timezone.utc).isoformat(),
        "remote_readback": {"readback_path": path, "payload_sha256": verified.payload_sha256}}
    raw = json.dumps(observation, sort_keys=True).encode()
    kwargs = {key: identity[key] for key in ("effect_id", "effect_type", "target_path", "target_digest", "adapter_idempotency_key")}
    kwargs.update(read_authority=read, verified_readback=verified, expected_revision=current["revision"], expected_attempt_count=current["attempt_count"], expected_authority_digest=current["authority_digest"], readback_id="actual-readback", verified_at=observation["verified_at"], artifact_content=raw, artifact_sha256=hashlib.sha256(raw).hexdigest())
    return current, goal, kwargs, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["body", "title", "native_kind"])
async def test_actual_get_cannot_mint_proof_for_unrelated_original_intent(client, async_db, monkeypatch, tmp_path, change):
    with pytest.raises((GitHubFollowthroughError, ValueError)):
        await observation_case(client, monkeypatch, tmp_path, semantic_change=change)


@pytest.mark.asyncio
async def test_stale_goal_can_append_actual_get_truth_but_cannot_finalize(client, async_db, monkeypatch, tmp_path):
    original, goal, kwargs, calls = await observation_case(client, monkeypatch, tmp_path)
    async with engine.get_session() as db:
        await db.execute(update(Goal).where(Goal.id == goal.id).values(revision=Goal.revision+1))
    observed = await jobs.record_github_recovery_observation(original["job_id"], **kwargs)
    assert observed["receipt"]["observation_only"] is True and observed["receipt"]["blocked_current_goal"] is True
    for field in ("status", "goal_revision", "authority_digest", "declared_authority", "deadline_at", "attempt_count", "lease", "result"):
        if field == "lease":
            assert observed[field]["owner"] == original[field]["owner"] and observed[field]["fencing_token"] == original[field]["fencing_token"]
        else: assert observed[field] == original[field]
    assert observed["effects"][0] == original["effects"][0]
    assert len(observed["artifacts"]) == len(original["artifacts"]) + 1 and calls == ["GET"]
    with pytest.raises(DurableJobError):
        await jobs.finalize_reconciled_job(original["job_id"], owner_kind="user", owner_principal_id=original["owner"]["principal_id"], expected_revision=observed["revision"], result={"status": "succeeded"})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "attempt", "authority", "effect", "type", "digest", "schema", "sealed_get", "trivial_callback"])
async def test_forged_or_stale_observation_has_no_append(client, async_db, monkeypatch, tmp_path, change):
    current, _, kwargs, calls = await observation_case(client, monkeypatch, tmp_path)
    if change == "revision": kwargs["expected_revision"] -= 1
    elif change == "attempt": kwargs["expected_attempt_count"] += 1
    elif change == "authority": kwargs["expected_authority_digest"] = "f"*64
    elif change == "effect": kwargs["effect_id"] = "nonexistent"
    elif change == "type": kwargs["effect_type"] = "repo_publication_blob_unbounded"
    elif change == "digest": kwargs["artifact_sha256"] = "0"*64
    elif change == "schema":
        kwargs["artifact_content"] = b"{}"; kwargs["artifact_sha256"] = hashlib.sha256(b"{}").hexdigest()
    elif change == "sealed_get": kwargs["verified_readback"] = {"verified": True}
    else: kwargs["read_authority"] = lambda: None
    with pytest.raises((DurableJobError, ValueError)):
        await jobs.record_github_recovery_observation(current["job_id"], **kwargs)
    unchanged = await jobs.get_job(current["job_id"])
    assert unchanged["revision"] == current["revision"] and unchanged["effects"] == current["effects"] and unchanged["artifacts"] == current["artifacts"]
    assert calls == ["GET"]
