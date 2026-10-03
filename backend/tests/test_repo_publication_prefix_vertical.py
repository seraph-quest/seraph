"""Real fixed Git producer and finite partial remote-object closure proof."""
import json
import uuid

import httpx
import pytest

from src.extensions.github_followthrough import GitHubFollowthroughService
from src.workflows.job_runtime import durable_job_repository as jobs
from tests.repo_publication_support import GitDataTransport
from tests.test_repo_publication_vertical import ORIGIN, actual_repair, request_for, selected_connection


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("lost_path", ["/git/blobs", "/git/trees", "/git/commits", "/git/refs"])
async def test_actual_remote_prefix_close_requires_positive_complete_inventory(
    client, async_db, monkeypatch, tmp_path, lost_path
):
    flow = await actual_repair(client, async_db, tmp_path, monkeypatch)
    connection = await selected_connection(client, flow, monkeypatch)
    transport = GitDataTransport(flow["repository"])
    lost, reject_get = {}, False
    def boundary(request):
        if reject_get and request.method == "GET":
            transport.calls.append(("GET", request.url.path, None))
            return httpx.Response(404, json={"message": "Not Found"})
        response = transport.handler(request)
        if request.method == "POST" and request.url.path.endswith(lost_path):
            lost.update(response.json())
            raise httpx.ReadTimeout("actual intercepted object accepted, response lost", request=request)
        return response
    async def resolver(*_args):
        return ["93.184.216.34"]
    adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(boundary))
    monkeypatch.setattr("src.workflows.repo_publication.GitHubFollowthroughService", lambda: adapter)
    prepared = await client.post("/api/capabilities/github/repo-publication/prepare",
        json=request_for(flow, connection), headers=ORIGIN)
    assert prepared.status_code == 200, prepared.text
    view = prepared.json(); job_id = view["job_id"]
    approved = await client.post(f"/api/approvals/{view['approval_id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    executed = await client.post(f"/api/capabilities/github/repo-publication/jobs/{job_id}/execute", headers=ORIGIN)
    assert executed.status_code == 200 and executed.json()["status"] == "unknown_external_effect", executed.text
    assert transport.calls[-1][0:2] == ("POST", lost_path)
    assert transport.pulls == {}
    post_count = sum(method == "POST" for method, *_ in transport.calls)
    async with async_db() as db:
        physical_engine = db.bind
    await physical_engine.dispose()
    adapter = type(adapter)(resolver=resolver, transport=httpx.MockTransport(boundary))
    stopped = await client.post("/api/capabilities/github/connection/revoke",
        json={"expected_revision": connection["revision"]}, headers=ORIGIN)
    assert stopped.status_code == 200, stopped.text
    row = await adapter._get_connection_row(flow["owner"].principal_id)
    original = await jobs.get_job(job_id)
    body = {"acknowledged_capacity_close": True, "expected_job_revision": original["revision"],
        "expected_connection_revision": row.revision, "expected_connection_fence": row.active_fence,
        "idempotency_key": str(uuid.uuid4())}
    if lost_path == "/git/commits":
        missing = await client.post(f"/api/capabilities/github/repo-publication/jobs/{job_id}/close-capacity", json=body, headers=ORIGIN)
        assert missing.status_code == 409 and "remote_commit_id_required" in missing.text, missing.text
        body["remote_commit_id"] = lost["sha"]
    before_row = row.model_dump(mode="json")
    reject_get = True
    absent = await client.post(f"/api/capabilities/github/repo-publication/jobs/{job_id}/close-capacity", json=body, headers=ORIGIN)
    assert absent.status_code == 409, absent.text
    assert await jobs.get_job(job_id) == original
    assert (await adapter._get_connection_row(flow["owner"].principal_id)).model_dump(mode="json") == before_row
    reject_get = False
    closed = await client.post(f"/api/capabilities/github/repo-publication/jobs/{job_id}/close-capacity", json=body, headers=ORIGIN)
    assert closed.status_code == 200, closed.text
    current = await jobs.get_job(job_id)
    assert current["status"] == "unknown_external_effect" and current["effects"] == original["effects"]
    assert current["github_capacity_closure"]["observation_only"] is True
    assert (await adapter._get_connection_row(flow["owner"].principal_id)).active_job_id is None
    assert sum(method == "POST" for method, *_ in transport.calls) == post_count
    calls_before_retry = list(transport.calls)
    repeated = await client.post(f"/api/capabilities/github/repo-publication/jobs/{job_id}/close-capacity", json=body, headers=ORIGIN)
    assert repeated.status_code == 200 and transport.calls == calls_before_retry
    proof = tmp_path / "actual-publication-prefix-close.json"
    proof.write_text(json.dumps({"intercepted_external_http": True, "lost_path": lost_path,
        "lost_response": lost, "original": original, "closed": closed.json(),
        "calls": transport.calls, "post_replay_count": 0, "restart": "file SQLite pool reopen/fresh adapter"}, sort_keys=True))
    print("ACTUAL_PUBLICATION_PREFIX_CLOSE=" + str(proof))
