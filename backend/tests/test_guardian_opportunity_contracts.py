"""Bounded public evidence and literal judgments, without any provider call."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from config.settings import settings
from src.guardian.opportunity_contracts import (
    GuardianPolicy, OpportunityEvidence, OpportunityError, digest, json_bytes, validate_assessment,
)
from src.guardian.opportunity_runtime import build_evidence, read_snapshot, stage_snapshot


def policy(**changes):
    current = datetime.now(timezone.utc)
    return dict(schema_version="seraph.guardian.policy.v1", assessment_enabled=True,
        confirmed_at=current, review_due_at=current + timedelta(hours=1), grant_id="reviewed-grant",
        original_root_id="root", goal_revision=1, source_watch_ids=[uuid4()],
        max_assessments_per_utc_day=2, **changes)


@pytest.mark.parametrize("field,value", [
    ("assessment_enabled", 1), ("auto_stage_plan", "false"),
    ("max_assessments_per_utc_day", True), ("max_assessments_per_utc_day", 5),
    ("max_plan_proposals_per_utc_day", 3), ("max_notification_per_utc_day", 3),
    ("minimum_gap_seconds", 1799), ("goal_revision", True), ("extra", "authority"),
])
def test_policy_rejects_coercion_and_unbounded_values(field, value):
    value_map = policy()
    value_map[field] = value
    with pytest.raises(ValidationError):
        GuardianPolicy.model_validate(value_map)


def observation(key="public-a", excerpt="line one\nline two", kind="public_https_text"):
    return SimpleNamespace(source=SimpleNamespace(source_key=key, kind=kind,
        identity_digest=digest(key.encode()), target="https://example.com/public"),
        after_excerpt=excerpt, new_hash=digest(b"new public body"))


def evidence(observations=None):
    packet = SimpleNamespace(id=str(uuid4()), observed_checkpoint_sha256=digest(b"checkpoint"),
        plan_revision=1, goal_revision=1)
    return build_evidence(packet=packet, observations=observations or [observation()])


def assessment(source):
    return dict(schema_version="seraph.opportunity.assessment.v1", relevance=3,
        confidence="medium", summary="A relevant public change", reason="Two offered lines support review.",
        citations=[dict(source_id=source.source_key, start_line=1, end_line=2,
            span_sha256=digest(source.excerpt.encode()))],
        suggested_blueprint="public-browser-check", abstain_reason=None)


def test_exact_citation_is_judgment_and_low_relevance_is_silent():
    offered = evidence()
    raw = assessment(offered.sources[0])
    assert validate_assessment(json_bytes(raw).decode(), offered).proposed
    raw["relevance"] = 2
    assert not validate_assessment(json_bytes(raw).decode(), offered).proposed


@pytest.mark.parametrize("mutation", ["invented_source", "hash", "range", "extra", "bool"])
def test_citations_and_closed_output_reject_invention(mutation):
    offered = evidence()
    raw = assessment(offered.sources[0])
    if mutation == "invented_source":
        raw["citations"][0]["source_id"] = "https://invented.example/instructions"
    elif mutation == "hash":
        raw["citations"][0]["span_sha256"] = "0" * 64
    elif mutation == "range":
        raw["citations"][0]["end_line"] = 3
    elif mutation == "extra":
        raw["tool_call"] = "run shell"
    else:
        raw["relevance"] = True
    with pytest.raises(ValueError):
        validate_assessment(json_bytes(raw).decode(), offered)


def test_duplicate_json_keys_are_not_adopted():
    with pytest.raises(ValueError, match="assessment_duplicate_key"):
        validate_assessment('{"relevance":3,"relevance":4}', evidence())


def test_evidence_is_sorted_public_whole_lf_lines_with_aggregate_byte_cap():
    offered = evidence([observation("z", "third"), observation("b", "é" * 1000 + "\r\nlast"),
        observation("a", "first\r\nsecond"), observation("private", "excluded", "workspace_text")])
    assert [source.source_key for source in offered.sources] == ["a", "b"]
    assert offered.sources[0].excerpt == "first\nsecond"
    assert "\r" not in offered.sources[1].excerpt
    assert sum(len(source.excerpt.encode()) for source in offered.sources) <= 4096
    assert len(offered.sources[1].excerpt.split("\n")) == 2
    with pytest.raises(OpportunityError, match="source_excerpt_unavailable"):
        evidence([observation(excerpt="é" * 3000)])


def test_snapshot_is_private_immutable_and_requires_original_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    offered = evidence()
    reference, sha = stage_snapshot(offered)
    path = tmp_path / reference
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert read_snapshot(reference, sha) == offered
    assert stage_snapshot(offered) == (reference, sha)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises((OpportunityError, ValueError)):
        read_snapshot(reference, sha)


def test_evidence_never_persists_raw_baseline_or_changed_text():
    offered = evidence()
    assert set(offered.sources[0].model_dump()) == {
        "source_key", "identity_digest", "target", "new_hash", "excerpt", "excerpt_sha256"}
    assert OpportunityEvidence.model_validate_json(offered.model_dump_json()) == offered
