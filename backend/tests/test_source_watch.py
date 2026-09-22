from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from src.db.models import Goal, GuardianDecisionPacket, GuardianSourceBaseline, GuardianSourceWatch
from src.guardian.source_watch import (
    CAPABILITY_ID,
    SourceWatchError,
    SourceObservation,
    SourceSpec,
    ScanResult,
    SourceWatchService,
    WatchCriteria,
    _baseline_only_observations,
    _partition_baseline_observations,
    _completion_notification_body,
    _failed_recovery_projection,
    _recovery_binding_error,
    _restore_prior_criteria,
    _goal_admission,
    _safe_source_projection,
    compute_input_digest,
    material_change,
    normalize_source_text,
    parse_criteria,
    parse_sources,
    redact_export_text,
)
import src.guardian.source_watch as source_watch_module
from src.security.http_transport import PinnedTransportError, fetch_pinned_https
from src.tools.filesystem_tool import _open_workspace_file
from config.settings import settings


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

    with pytest.raises(SourceWatchError) as secret_error:
        parse_sources([{"source_key": "secret", "kind": "workspace_text", "target": ".env"}])
    assert secret_error.value.code == "workspace_secret_path_blocked"

    with pytest.raises(SourceWatchError) as credential_error:
        parse_sources(
            [
                {
                    "source_key": "credentialed",
                    "kind": "public_https_text",
                    "target": "https://user:secret@example.com/status.txt",
                }
            ]
        )
    assert credential_error.value.code == "source_url_invalid"
    projected = _safe_source_projection(
        {
            "source_key": "legacy",
            "kind": "public_https_text",
            "target": (
                "https://user:secret@example.com/status.txt?token=raw-secret"
                "&x-api-key=raw-api-secret&x_api_key=raw-underscore-secret"
            ),
        }
    )
    assert "secret" not in projected["target"]
    assert "raw-secret" not in projected["target"]
    assert "raw-api-secret" not in projected["target"]
    assert "raw-underscore-secret" not in projected["target"]


def test_baseline_initialization_is_packet_free_and_notification_is_hash_only():
    source = parse_sources([{"source_key": "a", "kind": "workspace_text", "target": "a.txt"}])[0]
    first = SourceObservation(source, None, "new", "observed")
    rebaseline = SourceObservation(source, "old", "new", "observed", rebaseline=True)
    selected, status = _baseline_only_observations((first,))
    assert selected == (first,)
    assert status == "baseline_initialized"
    selected, status = _baseline_only_observations((rebaseline,))
    assert selected == (rebaseline,)
    assert status == "rebaseline_required"
    body = _completion_notification_body(
        packet_id="packet-1",
        status="succeeded",
        dossier_sha256="dossier-hash",
        task_sha256="task-hash",
    )
    assert "packet-1" in body
    assert "dossier-hash" in body and "task-hash" in body
    assert "source text" not in body


def test_mixed_scan_filters_baseline_only_sources_but_keeps_material_sources():
    first_source, changed_source = parse_sources(
        [
            {"source_key": "first", "kind": "workspace_text", "target": "first.txt"},
            {"source_key": "changed", "kind": "workspace_text", "target": "changed.txt"},
        ]
    )
    first = SourceObservation(
        source=first_source,
        old_hash=None,
        new_hash="first-new",
        status="observed",
        baseline_text="first content",
    )
    changed = SourceObservation(
        source=changed_source,
        old_hash="changed-old",
        new_hash="changed-new",
        status="observed",
        changed_lines=2,
        changed_chars=20,
        material=True,
        baseline_text="changed content",
    )
    scan = ScanResult(
        observations=(first, changed),
        material=(changed,),
        checkpoint={"schema": "test"},
        checkpoint_sha256="checkpoint",
        input_digest="input",
        baseline_updates=(first, changed),
        successful_sources=2,
        degraded=False,
    )
    baseline_only, status, action_scan = _partition_baseline_observations(
        scan,
        watch_id="watch-1",
        goal_revision=1,
        plan_revision=1,
        source_set_digest="sources",
        criteria_digest="criteria",
        capability_version=CAPABILITY_ID,
    )
    assert status == "baseline_initialized"
    assert baseline_only == (first,)
    assert action_scan.observations == (changed,)
    assert action_scan.material == (changed,)
    assert action_scan.baseline_updates == (changed,)
    # The action projection keeps the full occurrence digest so approval
    # recovery can rehydrate both the baseline-only and material sources.
    assert action_scan.input_digest == scan.input_digest
    assert len(action_scan.checkpoint["sources"]) == 2


def test_correction_undo_restores_only_matching_prior_criteria():
    current = {
        "include_terms": ["new"],
        "exclude_terms": [],
        "min_changed_lines": 1,
        "min_changed_chars": 1,
        "max_material_sources": 3,
    }
    restored = _restore_prior_criteria(
        current,
        prior_status="active",
        prior_before_json=json.dumps({**current, "include_terms": ["old"]}),
        prior_after_json=json.dumps(current),
    )
    assert restored["include_terms"] == ["old"]
    with pytest.raises(SourceWatchError) as stale:
        _restore_prior_criteria(
            {**current, "include_terms": ["different"]},
            prior_status="active",
            prior_before_json=json.dumps(current),
            prior_after_json=json.dumps(current),
        )
    assert stale.value.code == "correction_undo_target_stale"


def test_workspace_descriptor_rejects_hardlinks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "original.txt"
    original.write_text("bounded", encoding="utf-8")
    linked = workspace / "linked.txt"
    os.link(original, linked)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(ValueError, match="single-link"):
        with _open_workspace_file(linked.resolve(), flags=os.O_RDONLY):
            pass


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


def test_first_seen_source_only_initializes_baseline():
    source = parse_sources([{"source_key": "a", "kind": "workspace_text", "target": "a.txt"}])[0]
    criteria = parse_criteria({})
    observation = SourceObservation(
        source=source,
        old_hash=None,
        new_hash="new",
        status="observed",
        changed_lines=3,
        changed_chars=30,
        changed_text="deadline changed",
    )
    assert not material_change(observation, criteria)


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


def test_recovery_requires_every_verified_output_readback():
    dossier_path = "guardian/source-watches/watch/packets/packet.md"
    task_path = "guardian/source-watches/watch/tasks/packet.md"
    dossier_receipt = {
        "receipt_kind": "readback",
        "target_path": dossier_path,
        "status": "succeeded",
        "details": {"verified": True},
    }
    job = {"effects": [dossier_receipt]}
    assert not SourceWatchService._has_verified_output_effects(job, [dossier_path, task_path])
    job["effects"].append({**dossier_receipt, "target_path": task_path})
    assert SourceWatchService._has_verified_output_effects(job, [dossier_path, task_path])
    assert not SourceWatchService._has_verified_output_effects(job, [])


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

    goal.admission_budget_json = '{"reviewed_grant":true,"grant_id":"grant-1"}'
    admitted, reason, _ = _goal_admission(goal)
    assert not admitted
    assert reason == "goal_proactive_disabled"

    goal.proactive_enabled = True
    admitted, reason, _ = _goal_admission(goal)
    assert admitted
    assert reason == "admitted"


def test_packet_projection_redacts_secret_shaped_values():
    source = parse_sources([{"source_key": "a", "kind": "workspace_text", "target": "a.txt"}])[0]
    redacted, manifest = redact_export_text("api_key=super-secret-value")
    assert "super-secret-value" not in redacted
    assert manifest["replacement_count"] == 1
    observation = SourceObservation(
        source=source,
        old_hash="old",
        new_hash="new",
        status="observed",
        baseline_text="password=local-only-secret",
    )
    from src.guardian.source_watch import _observation_checkpoint

    checkpoint = _observation_checkpoint(observation)
    assert "baseline_text" not in checkpoint


def test_recovery_binding_requires_current_plan_and_source_set():
    source = parse_sources([{"source_key": "a", "kind": "workspace_text", "target": "a.txt"}])[0]
    sources_json = json.dumps(
        [
            {
                "source_key": source.source_key,
                "kind": source.kind,
                "target": source.target,
                "label": source.label,
                "priority": source.priority,
                "identity_digest": source.identity_digest,
            }
        ]
    )
    checkpoint = {
        "schema": "seraph.guardian.source-observation.v1",
        "sources": [{"source_key": "a", "identity_digest": source.identity_digest}],
    }
    job = {
        "job_id": "job-1",
        "job_kind": "guardian_source_watch",
        "goal_id": "goal-1",
        "goal_revision": 3,
        "plan_revision": 4,
        "declared_authority": {
            "goal_id": "goal-1",
            "goal_revision": 3,
            "plan_revision": 4,
            "source_set_digest": "sources-1",
            "criteria_digest": "criteria-1",
        },
    }
    packet = {
        "source_watch_id": "watch-1",
        "watch_id": "watch-1",
        "run_identity": "job-1",
        "goal_id": "goal-1",
        "goal_revision": 3,
        "plan_revision": 4,
        "criteria_digest": "criteria-1",
        "observed_checkpoint_json": json.dumps(checkpoint),
    }
    assert (
        _recovery_binding_error(
            watch_id="watch-1",
            goal_id="goal-1",
            goal_revision=3,
            plan_revision=4,
            source_set_digest="sources-1",
            criteria_digest="criteria-1",
            sources_json=sources_json,
            job_id="job-1",
            job=job,
            packet=packet,
        )
        is None
    )
    stale_job = {**job, "plan_revision": 5}
    assert (
        _recovery_binding_error(
            watch_id="watch-1",
            goal_id="goal-1",
            goal_revision=3,
            plan_revision=4,
            source_set_digest="sources-1",
            criteria_digest="criteria-1",
            sources_json=sources_json,
            job_id="job-1",
            job=stale_job,
            packet=packet,
        )
        == "durable_job_binding_mismatch"
    )
    stale_packet = {**packet, "plan_revision": 5}
    assert (
        _recovery_binding_error(
            watch_id="watch-1",
            goal_id="goal-1",
            goal_revision=3,
            plan_revision=4,
            source_set_digest="sources-1",
            criteria_digest="criteria-1",
            sources_json=sources_json,
            job_id="job-1",
            job=job,
            packet=stale_packet,
        )
        == "recovery_packet_binding_mismatch"
    )


@pytest.mark.asyncio
async def test_pinned_transport_rejects_private_resolution():
    async def private_resolver(_host: str, _port: int):
        return ["192.168.1.5"]

    with pytest.raises(PinnedTransportError):
        await fetch_pinned_https("https://example.com/status.txt", resolver=private_resolver)


@pytest.mark.asyncio
async def test_pinned_transport_bounds_dns_resolution():
    async def slow_resolver(_host: str, _port: int):
        await asyncio.sleep(0.05)
        return ["93.184.216.34"]

    with pytest.raises(TimeoutError):
        await fetch_pinned_https(
            "https://example.com/status.txt",
            resolver=slow_resolver,
            timeout_seconds=0.001,
        )


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
    stale, stale_status = await service._claim_watch(
        "watch-1",
        "job-stale",
        "occurrence-stale",
        expected_plan_revision=0,
    )
    assert stale is None
    assert stale_status == "watch_plan_revision_stale"
    claimed, status = await service._claim_watch(
        "watch-1",
        "job-1",
        "occurrence-1",
        expected_plan_revision=1,
    )
    assert claimed is not None
    assert status == "claimed"
    assert not await service._release_watch("watch-1", "job-other", 1, "succeeded")
    assert not await service._release_watch("watch-1", "job-1", 2, "succeeded")
    duplicate, duplicate_status = await service._claim_watch(
        "watch-1",
        "job-2",
        "occurrence-2",
        expected_plan_revision=1,
    )
    assert duplicate is None
    assert duplicate_status == "watch_active_or_not_admissible"
    assert await service._release_watch("watch-1", "job-1", 1, "succeeded")


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


@pytest.mark.asyncio
async def test_recovery_baseline_gate_blocks_missing_canonical_baseline(async_db):
    source = parse_sources(
        [{"source_key": "local", "kind": "workspace_text", "target": "notes.txt"}]
    )[0]
    watch = GuardianSourceWatch(
        id="watch-recovery-baseline",
        goal_id="goal-recovery-baseline",
        owner_principal_id="operator",
        owner_session_id="session",
        scheduled_job_id="scheduled-recovery-baseline",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_ID,
        sources_json=json.dumps(
            [
                {
                    "source_key": source.source_key,
                    "kind": source.kind,
                    "target": source.target,
                    "label": source.label,
                    "priority": source.priority,
                    "identity_digest": source.identity_digest,
                }
            ]
        ),
        criteria_json="{}",
        source_set_digest="sources",
        criteria_digest="criteria",
        goal_revision=1,
        plan_revision=1,
    )
    observation = SourceObservation(
        source=source,
        old_hash="old",
        new_hash="new",
        status="observed",
    )
    scan = ScanResult(
        observations=(observation,),
        material=(observation,),
        checkpoint={},
        checkpoint_sha256="checkpoint",
        input_digest="input",
        baseline_updates=(observation,),
        successful_sources=1,
        degraded=False,
    )
    packet = GuardianDecisionPacket(
        source_watch_id=watch.id,
        watch_id=watch.id,
        goal_id=watch.goal_id,
        run_identity="job-recovery-baseline",
    )
    with pytest.raises(SourceWatchError) as error:
        await SourceWatchService()._repair_recovery_baselines(watch, packet, scan)
    assert error.value.code == "recovery_baseline_unverified"


@pytest.mark.asyncio
async def test_preclaim_packet_failure_blocks_job_and_releases_watch(monkeypatch):
    watch = GuardianSourceWatch(
        id="watch-preclaim",
        goal_id="goal-preclaim",
        owner_principal_id="operator",
        owner_session_id="session",
        scheduled_job_id="scheduled-preclaim",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_ID,
        state="active",
        active_job_id="job-preclaim",
        active_job_fence=7,
        plan_revision=1,
    )
    packet = GuardianDecisionPacket(
        id="packet-preclaim",
        source_watch_id=watch.id,
        watch_id=watch.id,
        goal_id=watch.goal_id,
        run_identity="job-preclaim",
    )
    service = SourceWatchService()
    transition_calls: list[dict[str, object]] = []
    release_calls: list[tuple[object, ...]] = []

    async def transition(job_id, status, **kwargs):
        transition_calls.append({"job_id": job_id, "status": status, **kwargs})
        return {"job_id": job_id, "status": status}

    async def mark_packet(packet_id, reason):
        transition_calls.append({"packet_id": packet_id, "packet_reason": reason})

    async def release_watch(*args):
        release_calls.append(args)
        return True

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(source_watch_module.durable_job_repository, "transition_job", transition)
    monkeypatch.setattr(service, "_mark_packet_failure", mark_packet)
    monkeypatch.setattr(service, "_release_watch", release_watch)
    monkeypatch.setattr(source_watch_module, "_audit_watch_event", audit)

    result = await service._settle_preclaim_packet_failure(
        watch,
        packet,
        {"job_id": packet.run_identity, "status": "awaiting_approval", "revision": 4},
        watch_fence=watch.active_job_fence,
        reason_code="recovery_baseline_changed",
    )

    assert result["status"] == "blocked"
    assert result["memory_status"] == "no_learning"
    assert result["durable_status"] == "blocked"
    assert result["watch_released"] is True
    assert transition_calls[0]["status"] == "blocked"
    assert transition_calls[0]["expected_state"] == "awaiting_approval"
    assert transition_calls[0]["expected_revision"] == 4
    assert release_calls == [(watch.id, packet.run_identity, 7, "blocked", "recovery_baseline_changed")]


@pytest.mark.asyncio
async def test_preclaim_settlement_cas_failure_keeps_live_approval_and_watch(monkeypatch):
    watch = GuardianSourceWatch(
        id="watch-preclaim-live",
        goal_id="goal-preclaim-live",
        owner_principal_id="operator",
        owner_session_id="session",
        scheduled_job_id="scheduled-preclaim-live",
        capability_id=CAPABILITY_ID,
        capability_version=CAPABILITY_ID,
        state="active",
        active_job_id="job-preclaim-live",
        active_job_fence=8,
        plan_revision=1,
    )
    packet = GuardianDecisionPacket(
        id="packet-preclaim-live",
        source_watch_id=watch.id,
        watch_id=watch.id,
        goal_id=watch.goal_id,
        run_identity="job-preclaim-live",
    )
    service = SourceWatchService()
    mark_calls: list[tuple[object, ...]] = []
    release_calls: list[tuple[object, ...]] = []

    async def transition(*_args, **_kwargs):
        raise RuntimeError("storage_cas_unavailable")

    async def get_job(_job_id):
        return {"job_id": packet.run_identity, "status": "awaiting_approval", "revision": 5}

    async def mark_packet(*args):
        mark_calls.append(args)

    async def release_watch(*args):
        release_calls.append(args)
        return True

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(source_watch_module.durable_job_repository, "transition_job", transition)
    monkeypatch.setattr(source_watch_module.durable_job_repository, "get_job", get_job)
    monkeypatch.setattr(service, "_mark_packet_failure", mark_packet)
    monkeypatch.setattr(service, "_release_watch", release_watch)
    monkeypatch.setattr(source_watch_module, "_audit_watch_event", audit)

    result = await service._settle_preclaim_packet_failure(
        watch,
        packet,
        {"job_id": packet.run_identity, "status": "awaiting_approval", "revision": 4},
        watch_fence=watch.active_job_fence,
        reason_code="recovery_baseline_changed",
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "approval_settlement_required"
    assert result["durable_status"] == "awaiting_approval"
    assert result["watch_released"] is False
    assert mark_calls == []
    assert release_calls == []


def test_failed_recovery_projection_preserves_uncertain_durable_status():
    unknown = _failed_recovery_projection("unknown_external_effect")
    assert unknown["status"] == "unknown_external_effect"
    assert unknown["watch_status"] == "blocked"
    assert unknown["recovery"] == "failed_occurrence_reconciliation_required"
    assert unknown["reason_code"] == "unknown_external_effect_pending_reconciliation"

    liability = _failed_recovery_projection("cost_liability")
    assert liability["status"] == "cost_liability"
    assert liability["watch_status"] == "blocked"

    cancelled = _failed_recovery_projection("cancelled")
    assert cancelled["status"] == "cancelled"
    assert cancelled["watch_status"] == "cancelled"
