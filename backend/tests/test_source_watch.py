from __future__ import annotations

import httpx
import pytest

from src.db.models import Goal, GuardianSourceBaseline, GuardianSourceWatch
from src.guardian.source_watch import (
    CAPABILITY_ID,
    SourceObservation,
    SourceSpec,
    SourceWatchService,
    WatchCriteria,
    _goal_admission,
    compute_input_digest,
    material_change,
    normalize_source_text,
    parse_criteria,
    parse_sources,
)
from src.security.http_transport import PinnedTransportError, fetch_pinned_https


def test_source_contract_is_bounded_and_identity_is_stable():
    source = parse_sources(
        [
            {
                "source_key": "status",
                "kind": "public_https_text",
                "target": "https://example.com/status.txt",
                "priority": 5,
            }
        ]
    )[0]
    assert CAPABILITY_ID == "guardian.research-watch.v1"
    assert source.identity_digest
    with pytest.raises(ValueError):
        parse_sources(
            [
                {
                    "source_key": str(index),
                    "kind": "public_https_text",
                    "target": f"https://example.com/{index}.txt",
                }
                for index in range(11)
            ]
        )


def test_normalization_and_material_change_use_casefolded_changed_lines():
    assert normalize_source_text("<p>A</p><script>bad()</script><p>B</p>", html_content=True) == "A\nB"
    criteria = parse_criteria(
        {
            "include_terms": ["Deadline"],
            "exclude_terms": ["marketing"],
            "min_changed_lines": 2,
            "min_changed_chars": 1000,
            "max_material_sources": 2,
        }
    )
    source = parse_sources(
        [{"source_key": "a", "kind": "workspace_text", "target": "a.txt", "priority": 4}]
    )[0]
    observation = SourceObservation(
        source=source,
        old_hash="old",
        new_hash="new",
        status="observed",
        changed_lines=2,
        changed_chars=20,
        before_excerpt="old",
        after_excerpt="NEW DEADLINE",
    )
    assert material_change(observation, criteria)
    excluded = SourceObservation(**{**observation.__dict__, "after_excerpt": "marketing deadline"})
    assert not material_change(excluded, criteria)


def test_input_digest_binds_old_and_new_observations():
    source = parse_sources(
        [{"source_key": "a", "kind": "workspace_text", "target": "a.txt"}]
    )[0]
    first = SourceObservation(source, "old", "new", "observed")
    second = SourceObservation(source, "old", "other", "observed")
    digest_one = compute_input_digest(
        watch_id="watch",
        goal_revision=1,
        plan_revision=1,
        source_set_digest="sources",
        criteria_digest="criteria",
        capability_version=CAPABILITY_ID,
        observations=[first],
    )
    digest_two = compute_input_digest(
        watch_id="watch",
        goal_revision=1,
        plan_revision=1,
        source_set_digest="sources",
        criteria_digest="criteria",
        capability_version=CAPABILITY_ID,
        observations=[second],
    )
    assert digest_one != digest_two


def test_goal_admission_requires_reviewed_budget_before_source_io():
    goal = Goal(
        title="Track the release",
        owner_principal_id="operator",
        owner_session_id="session",
        admission_budget_json='{"reviewed_grant":false}',
    )
    admitted, reason, budget = _goal_admission(goal)
    assert not admitted
    assert reason == "goal_budget_missing_reviewed_grant"
    assert budget is not None

    goal.admission_budget_json = (
        '{"reviewed_grant":true,"grant_id":"grant-1",'
        '"period_started_at":"2099-01-01T00:00:00+00:00"}'
    )
    admitted, reason, _ = _goal_admission(goal)
    assert not admitted
    assert reason == "goal_budget_period_not_started"


@pytest.mark.asyncio
async def test_pinned_transport_rejects_private_resolution():
    async def private_resolver(_host: str, _port: int):
        return ["192.168.1.5"]

    with pytest.raises(PinnedTransportError):
        await fetch_pinned_https("https://example.com/status.txt", resolver=private_resolver)


@pytest.mark.asyncio
async def test_pinned_transport_caps_response_and_keeps_logical_url_for_mock():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    async def resolver(_host: str, _port: int):
        return ["93.184.216.34"]

    response = await fetch_pinned_https(
        "https://example.com/status.txt",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    assert response.content == b"ok"
    assert seen == ["https://example.com/status.txt"]
    assert response.pinned_address == "93.184.216.34"


@pytest.mark.asyncio
async def test_watch_fence_allows_one_claim(async_db):
    watch = GuardianSourceWatch(
        id="watch-1",
        goal_id="goal-1",
        owner_principal_id="operator",
        owner_session_id="session",
        scheduled_job_id="scheduled-1",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_ID,
        sources_json="[]",
        criteria_json='{"include_terms":[],"exclude_terms":[],"min_changed_lines":1,"min_changed_chars":1,"max_material_sources":1}',
        schedule_spec_json='{"cron":"*/30 * * * *","timezone":"UTC"}',
    )
    async with async_db() as db:
        db.add(watch)
    service = SourceWatchService()
    claimed, status = await service._claim_watch("watch-1", "job-1", "occurrence-1")
    assert claimed is not None
    assert status == "claimed"
    duplicate, duplicate_status = await service._claim_watch("watch-1", "job-2", "occurrence-2")
    assert duplicate is None
    assert duplicate_status == "watch_active_or_not_admissible"


@pytest.mark.asyncio
async def test_scan_uses_injected_source_and_preserves_old_hash(async_db):
    source = parse_sources(
        [{"source_key": "local", "kind": "workspace_text", "target": "notes.txt", "priority": 5}]
    )[0]
    criteria = parse_criteria(
        {
            "include_terms": ["deadline"],
            "exclude_terms": [],
            "min_changed_lines": 1,
            "min_changed_chars": 1,
            "max_material_sources": 1,
        }
    )
    watch = GuardianSourceWatch(
        id="watch-2",
        goal_id="goal-2",
        owner_principal_id="operator",
        owner_session_id="session",
        scheduled_job_id="scheduled-2",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_ID,
        sources_json='[{"source_key":"local","kind":"workspace_text","target":"notes.txt","label":"Local","priority":5,"identity_digest":"%s"}]' % source.identity_digest,
        criteria_json='{"include_terms":["deadline"],"exclude_terms":[],"min_changed_lines":1,"min_changed_chars":1,"max_material_sources":1}',
        schedule_spec_json='{"cron":"*/30 * * * *","timezone":"UTC"}',
        source_set_digest="sources",
        criteria_digest="criteria",
        goal_revision=1,
        plan_revision=1,
    )
    baseline = GuardianSourceBaseline(
        watch_id="watch-2",
        source_key="local",
        kind=source.kind,
        target=source.target,
        identity_digest=source.identity_digest,
        baseline_text="old plan",
        baseline_sha256="old",
        state="ready",
    )
    async with async_db() as db:
        db.add(watch)
        db.add(baseline)
    async def fetcher(_source: SourceSpec):
        return "new deadline plan", {}
    service = SourceWatchService(fetcher=fetcher)
    result = await service._scan(watch)
    assert result.successful_sources == 1
    assert result.material[0].source.source_key == "local"
    assert result.observations[0].old_hash == "old"
    assert result.observations[0].new_hash
