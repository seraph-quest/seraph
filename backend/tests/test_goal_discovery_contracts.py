from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote
from uuid import uuid4

import httpx
import pytest

from src.guardian.research_plan_contracts import STAGES, validate_goal_research_plan, SearchManifestV1, SourceSelectionV1
from src.guardian.discovery_search import DiscoverySearch, DiscoverySearchBlocked, parse_search_html


def valid_plan():
    now = datetime.now(timezone.utc)
    generation = uuid4()
    inputs = [{"artifact_id": "public-brief", "digest": "a" * 64, "schema_version": 1}]
    steps = []
    for identifier, capability, slots in STAGES:
        steps.append({"step_id": identifier, "capability_id": capability, "capability_version": 1,
            "input_refs": deepcopy(inputs), "output_slots": [{"slot": slot, "artifact_type": kind, "max_bytes": 65536} for slot, kind in slots]})
        inputs = [{"producer_step_id": identifier, "output_slot": slot, "json_pointer": ""} for slot, _ in slots]
    return {"schema_version": 1, "plan_id": str(uuid4()), "programme_id": str(generation),
        "programme_revision": 1, "goal_id": str(uuid4()), "goal_revision": 1, "grant_id": generation.hex,
        "grant_revision": 1, "public_brief_digest": "a" * 64, "route_epoch": 1,
        "strategy_binding": {"status": "none"}, "issued_at": now.isoformat(),
        "deadline_at": (now + timedelta(seconds=300)).isoformat(), "idempotency_key": str(uuid4()),
        "limits": {"max_queries": 3, "max_results": 15, "max_sources": 4, "max_inference_requests": 4,
            "max_wall_seconds": 300, "max_search_seconds": 20, "max_search_bytes": 524288,
            "max_source_bytes": 262144, "max_output_bytes": 1048576, "cost_limit_microusd": 1000}, "steps": steps}


def test_exact_native_plan_roundtrip_and_two_bound_extraction_inputs():
    plan = validate_goal_research_plan(valid_plan())
    assert validate_goal_research_plan(plan.model_dump(mode="json")) == plan
    assert [r.output_slot for r in plan.steps[2].input_refs] == ["manifest", "selection"]


@pytest.mark.parametrize("identifier", ["34d4b6ea", "setup" + "f" * 64, "legacy:Goal_1"])
def test_canonical_goal_reference_preserves_existing_owner_ids(identifier):
    value = valid_plan()
    value["goal_id"] = identifier
    plan = validate_goal_research_plan(value)
    assert plan.goal_id == identifier
    assert validate_goal_research_plan(plan.model_dump(mode="json")).goal_id == identifier


@pytest.mark.parametrize("identifier", ["", " x", "x ", "../goal", "https://example.com", "x" * 129, 1, True])
def test_canonical_goal_reference_rejects_unsafe_or_normalized_aliases(identifier):
    value = valid_plan()
    value["goal_id"] = identifier
    with pytest.raises(ValueError):
        validate_goal_research_plan(value)


def test_native_structural_refs_are_preserved_only_for_exact_service_branch():
    from src.work_board.research_parent import GoalDiscoveryInputs
    from src.workflows.job_runtime import _safe_durable_inputs
    value = {"plan_ref": {"artifact_id": "art_owned", "digest": "a" * 64, "schema_version": 1},
        "plan_file_path": "goal-programmes/" + "b" * 32 + "/" + "c" * 64 + "-" + "a" * 64 + ".json",
        "public_brief_ref": {"artifact_id": "art_brief", "digest": "d" * 64, "schema_version": 1},
        "public_brief_file_path": "goal-programmes/" + "b" * 32 + "/" + "e" * 64 + "-" + "d" * 64 + ".json",
        "no_learning": True}
    _, original = _safe_durable_inputs(value)
    assert original["redacted"] is True and "plan_ref" not in original
    _, native = _safe_durable_inputs(value, discovery=True)
    assert native == value and GoalDiscoveryInputs.model_validate(value).model_dump(mode="json") == value
    for key, changed in [("callback", "execute"), ("public_brief", "raw public or private content"),
            ("url", "https://example.com"), ("no_learning", 1), ("plan_file_path", "goal-programmes/../wrong.json")]:
        with pytest.raises(ValueError):
            _safe_durable_inputs({**value, key: changed}, discovery=True)


def test_brief_is_closed_six_field_native_coverage_and_inert_proposals():
    from src.guardian.research_plan_contracts import DiscoveryBriefV1
    value = {"findings": [], "citations": [], "uncertainties": [], "prepared_artifact_refs": [],
        "proposed_next_steps": [], "coverage": {"original_public_brief_digest": "a" * 64,
            "original_public_brief_byte_count": 12, "public_brief_fully_represented": False,
            "outcome_state": "empty", "status": "unsupported", "sources": [], "source_spans": [], "unavailable": [],
            "native_verified": True, "semantic_truth_verified": False, "no_learning": True}}
    assert DiscoveryBriefV1.model_validate(value).model_dump(mode="json") == value
    for key, changed in [("callback", "execute"), ("tools", ["write"]), ("proposed_next_steps", [{"kind": "publish"}])]:
        with pytest.raises(ValueError):
            DiscoveryBriefV1.model_validate({**value, key: changed})


@pytest.mark.parametrize("change", [
    lambda p: p.update(callback="execute"),
    lambda p: p.update(goal_revision=True),
    lambda p: p.update(route_epoch=1.0),
    lambda p: p.update(schema_version=True),
    lambda p: p.update(grant_id=str(uuid4())),
    lambda p: p.update(programme_revision=2),
    lambda p: p.update(issued_at="2026-10-08T00:00:00"),
    lambda p: p.update(deadline_at="2026-10-08T00:00:00Z"),
    lambda p: p["limits"].update(max_inference_requests=5),
    lambda p: p["limits"].update(max_source_bytes=262145),
    lambda p: p["limits"].update(cost_limit_microusd=-1),
    lambda p: p["steps"].reverse(),
    lambda p: p["steps"][0].update(capability_id="arbitrary.execute"),
    lambda p: p["steps"][0].update(capability_version=True),
    lambda p: p["steps"][0].update(input_refs=[{"url": "https://example.com"}]),
    lambda p: p["steps"][0].update(input_refs=[{"producer_step_id": "prepare_brief", "output_slot": "brief", "json_pointer": ""}]),
    lambda p: p["steps"][2].update(input_refs=[{"producer_step_id": "search_public", "output_slot": "manifest", "json_pointer": ""}]),
    lambda p: p["steps"][1]["output_slots"][1].update(slot="other"),
    lambda p: p["steps"][1]["input_refs"][0].update(json_pointer="/bad~escape"),
    lambda p: p["steps"][1]["input_refs"][0].update(json_pointer="/queries/0"),
    lambda p: p["steps"][1].update(input_refs=[p["steps"][0]["input_refs"][0]]),
    lambda p: p["steps"][3].update(input_refs=[{"producer_step_id": "search_public", "output_slot": "manifest", "json_pointer": ""}]),
    lambda p: p["steps"][2]["input_refs"].reverse(),
])
def test_plan_authority_and_graph_mutations_are_rejected(change):
    value = valid_plan()
    change(value)
    with pytest.raises(ValueError):
        validate_goal_research_plan(value)


@pytest.mark.asyncio
async def test_fixed_search_transport_and_manifest_duplicate_provenance():
    contacts = []
    checks = []
    async def authority():
        checks.append(True)
    async def resolver(host, port):
        assert (host, port) == ("html.duckduckgo.com", 443)
        return ["93.184.216.34"]
    async def handle(request):
        contacts.append(request)
        assert request.method == "POST"
        assert request.headers["host"] == "html.duckduckgo.com"
        assert not any(key in request.headers for key in ("authorization", "cookie", "proxy-authorization"))
        assert parse_qs(request.content.decode(), keep_blank_values=True) == {"q": ["reviewed public query"], "b": [""], "kl": ["us-en"]}
        target = quote("https://example.com/article", safe="")
        return httpx.Response(200, headers={"content-type": "text/html"}, text=f'<a class="result__a" href="//duckduckgo.com/l/?uddg={target}">Public article</a>')
    search = DiscoverySearch(resolver=resolver, transport=httpx.MockTransport(handle))
    result = await search.search(["reviewed public query"] * 3, run_id=uuid4(), authority_check=authority)
    manifest = SearchManifestV1.model_validate(result.manifest)
    assert len(contacts) == 3 and checks
    assert len(manifest.results) == 1 and manifest.results[0].exact_url == "https://example.com/article"
    assert len(manifest.results[0].result_id) == 32
    ref = {"artifact_id": "exact-manifest", "digest": "a" * 64, "schema_version": 1}
    selection = SourceSelectionV1(run_id=manifest.run_id, manifest_ref=ref, selected_result_ids=[manifest.results[0].result_id])
    selection.validate_manifest(manifest, selection.manifest_ref)
    with pytest.raises(ValueError):
        SourceSelectionV1(run_id=manifest.run_id, manifest_ref=ref, selected_result_ids=["b" * 32]).validate_manifest(manifest, selection.manifest_ref)


@pytest.mark.parametrize("raw", [b"<html>unexpected blank response</html>", b'<form id="challenge-form"></form>', b'<a class="result__a" href="https://user:secret@example.com/">Denied</a>', b'<a class="result__a" href="http://localhost/">Denied</a>', b'<a class="result__a" href="https://example.com/">unfinished', b"\xff", b"x" * 524289])
def test_search_drift_captcha_and_malicious_urls_fail_closed(raw):
    with pytest.raises(DiscoverySearchBlocked):
        parse_search_html(raw)


def test_only_recognized_no_results_is_empty_success():
    assert parse_search_html(b'<div class="no-results">No results found.</div>') == []
